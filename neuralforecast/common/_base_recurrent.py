# AUTOGENERATED! DO NOT EDIT! File to edit: ../../nbs/common.base_recurrent.ipynb.

# %% auto 0
__all__ = ['BaseRecurrent']

# %% ../../nbs/common.base_recurrent.ipynb 6
import numpy as np
import torch
import torch.nn as nn
import pytorch_lightning as pl
import neuralforecast.losses.pytorch as losses

from ._base_model import BaseModel
from ._scalers import TemporalNorm
from ..tsdataset import TimeSeriesDataModule
from ..utils import get_indexer_raise_missing

# %% ../../nbs/common.base_recurrent.ipynb 7
class BaseRecurrent(BaseModel):
    """Base Recurrent

    Base class for all recurrent-based models. The forecasts are produced sequentially between
    windows.

    This class implements the basic functionality for all windows-based models, including:
    - PyTorch Lightning's methods training_step, validation_step, predict_step. <br>
    - fit and predict methods used by NeuralForecast.core class. <br>
    - sampling and wrangling methods to sequential windows. <br>
    """

    def __init__(
        self,
        h,
        input_size,
        inference_input_size,
        loss,
        valid_loss,
        learning_rate,
        max_steps,
        val_check_steps,
        batch_size,
        valid_batch_size,
        scaler_type="robust",
        num_lr_decays=0,
        early_stop_patience_steps=-1,
        futr_exog_list=None,
        hist_exog_list=None,
        stat_exog_list=None,
        drop_last_loader=False,
        random_seed=1,
        alias=None,
        optimizer=None,
        optimizer_kwargs=None,
        lr_scheduler=None,
        lr_scheduler_kwargs=None,
        dataloader_kwargs=None,
        **trainer_kwargs,
    ):
        super().__init__(
            random_seed=random_seed,
            loss=loss,
            valid_loss=valid_loss,
            optimizer=optimizer,
            optimizer_kwargs=optimizer_kwargs,
            lr_scheduler=lr_scheduler,
            lr_scheduler_kwargs=lr_scheduler_kwargs,
            futr_exog_list=futr_exog_list,
            hist_exog_list=hist_exog_list,
            stat_exog_list=stat_exog_list,
            max_steps=max_steps,
            early_stop_patience_steps=early_stop_patience_steps,
            **trainer_kwargs,
        )

        # Padder to complete train windows,
        # example y=[1,2,3,4,5] h=3 -> last y_output = [5,0,0]
        self.h = h
        self.input_size = input_size
        self.inference_input_size = inference_input_size
        self.padder = nn.ConstantPad1d(padding=(0, self.h), value=0.0)

        unsupported_distributions = ["Bernoulli", "ISQF"]
        if (
            isinstance(self.loss, losses.DistributionLoss)
            and self.loss.distribution in unsupported_distributions
        ):
            raise Exception(
                f"Distribution {self.loss.distribution} not available for Recurrent-based models. Please choose another distribution."
            )

        # Valid batch_size
        self.batch_size = batch_size
        if valid_batch_size is None:
            self.valid_batch_size = batch_size
        else:
            self.valid_batch_size = valid_batch_size

        # Optimization
        self.learning_rate = learning_rate
        self.max_steps = max_steps
        self.num_lr_decays = num_lr_decays
        self.lr_decay_steps = (
            max(max_steps // self.num_lr_decays, 1) if self.num_lr_decays > 0 else 10e7
        )
        self.early_stop_patience_steps = early_stop_patience_steps
        self.val_check_steps = val_check_steps

        # Scaler
        self.scaler = TemporalNorm(
            scaler_type=scaler_type,
            dim=-1,  # Time dimension is -1.
            num_features=1 + len(self.hist_exog_list) + len(self.futr_exog_list),
        )

        # Fit arguments
        self.val_size = 0
        self.test_size = 0

        # DataModule arguments
        self.dataloader_kwargs = dataloader_kwargs
        self.drop_last_loader = drop_last_loader
        # used by on_validation_epoch_end hook
        self.validation_step_outputs = []
        self.alias = alias

    def _normalization(self, batch, val_size=0, test_size=0):
        temporal = batch["temporal"]  # B, C, T
        temporal_cols = batch["temporal_cols"].copy()
        y_idx = batch["y_idx"]

        # Separate data and mask
        temporal_data_cols = self._get_temporal_exogenous_cols(
            temporal_cols=temporal_cols
        )
        temporal_idxs = get_indexer_raise_missing(temporal_cols, temporal_data_cols)
        temporal_idxs = np.append(y_idx, temporal_idxs)
        temporal_data = temporal[:, temporal_idxs, :]
        temporal_mask = temporal[:, temporal_cols.get_loc("available_mask"), :].clone()

        # Remove validation and test set to prevent leakeage
        if val_size + test_size > 0:
            cutoff = val_size + test_size
            temporal_mask[:, -cutoff:] = 0

        # Normalize. self.scaler stores the shift and scale for inverse transform
        temporal_mask = temporal_mask.unsqueeze(
            1
        )  # Add channel dimension for scaler.transform.
        temporal_data = self.scaler.transform(x=temporal_data, mask=temporal_mask)

        # Replace values in windows dict
        temporal[:, temporal_idxs, :] = temporal_data
        batch["temporal"] = temporal

        return batch

    def _inv_normalization(self, y_hat, temporal_cols, y_idx):
        # Receives window predictions [B, seq_len, H, output]
        # Broadcasts outputs and inverts normalization

        # Get 'y' scale and shift, and add W dimension
        y_loc = self.scaler.x_shift[:, [y_idx], 0].flatten()  # [B,C,T] -> [B]
        y_scale = self.scaler.x_scale[:, [y_idx], 0].flatten()  # [B,C,T] -> [B]

        # Expand scale and shift to y_hat dimensions
        y_loc = y_loc.view(*y_loc.shape, *(1,) * (y_hat.ndim - 1))  # .expand(y_hat)
        y_scale = y_scale.view(
            *y_scale.shape, *(1,) * (y_hat.ndim - 1)
        )  # .expand(y_hat)

        y_hat = self.scaler.inverse_transform(z=y_hat, x_scale=y_scale, x_shift=y_loc)

        return y_hat, y_loc, y_scale

    def _create_windows(self, batch, step):
        temporal = batch["temporal"]
        temporal_cols = batch["temporal_cols"]

        if step == "train":
            if self.val_size + self.test_size > 0:
                cutoff = -self.val_size - self.test_size
                temporal = temporal[:, :, :cutoff]
            temporal = self.padder(temporal)

            # Truncate batch to shorter time-series
            av_condition = torch.nonzero(
                torch.min(
                    temporal[:, temporal_cols.get_loc("available_mask")], axis=0
                ).values
            )
            min_time_stamp = int(av_condition.min())

            available_ts = temporal.shape[-1] - min_time_stamp
            if available_ts < 1 + self.h:
                raise Exception(
                    "Time series too short for given input and output size. \n"
                    f"Available timestamps: {available_ts}"
                )

            temporal = temporal[:, :, min_time_stamp:]

        if step == "val":
            if self.test_size > 0:
                temporal = temporal[:, :, : -self.test_size]
            temporal = self.padder(temporal)

        if step == "predict":
            if (self.test_size == 0) and (len(self.futr_exog_list) == 0):
                temporal = self.padder(temporal)

            # Test size covers all data, pad left one timestep with zeros
            if temporal.shape[-1] == self.test_size:
                padder_left = nn.ConstantPad1d(padding=(1, 0), value=0.0)
                temporal = padder_left(temporal)

        # Parse batch
        window_size = 1 + self.h  # 1 for current t and h for future
        windows = temporal.unfold(dimension=-1, size=window_size, step=1)

        # Truncated backprogatation/inference (shorten sequence where RNNs unroll)
        n_windows = windows.shape[2]
        input_size = -1
        if (step == "train") and (self.input_size > 0):
            input_size = self.input_size
            if (input_size > 0) and (n_windows > input_size):
                max_sampleable_time = n_windows - self.input_size + 1
                start = np.random.choice(max_sampleable_time)
                windows = windows[:, :, start : (start + input_size), :]

        if (step == "val") and (self.inference_input_size > 0):
            cutoff = self.inference_input_size + self.val_size
            windows = windows[:, :, -cutoff:, :]

        if (step == "predict") and (self.inference_input_size > 0):
            cutoff = self.inference_input_size + self.test_size
            windows = windows[:, :, -cutoff:, :]

        # [B, C, input_size, 1+H]
        windows_batch = dict(
            temporal=windows,
            temporal_cols=temporal_cols,
            static=batch.get("static", None),
            static_cols=batch.get("static_cols", None),
        )

        return windows_batch

    def _parse_windows(self, batch, windows):
        # [B, C, seq_len, 1+H]
        # Filter insample lags from outsample horizon
        mask_idx = batch["temporal_cols"].get_loc("available_mask")
        y_idx = batch["y_idx"]
        insample_y = windows["temporal"][:, y_idx, :, : -self.h]
        insample_mask = windows["temporal"][:, mask_idx, :, : -self.h]
        outsample_y = windows["temporal"][:, y_idx, :, -self.h :].contiguous()
        outsample_mask = windows["temporal"][:, mask_idx, :, -self.h :].contiguous()

        # Filter historic exogenous variables
        if len(self.hist_exog_list):
            hist_exog_idx = get_indexer_raise_missing(
                windows["temporal_cols"], self.hist_exog_list
            )
            hist_exog = windows["temporal"][:, hist_exog_idx, :, : -self.h]
        else:
            hist_exog = None

        # Filter future exogenous variables
        if len(self.futr_exog_list):
            futr_exog_idx = get_indexer_raise_missing(
                windows["temporal_cols"], self.futr_exog_list
            )
            futr_exog = windows["temporal"][:, futr_exog_idx, :, :]
        else:
            futr_exog = None
        # Filter static variables
        if len(self.stat_exog_list):
            static_idx = get_indexer_raise_missing(
                windows["static_cols"], self.stat_exog_list
            )
            stat_exog = windows["static"][:, static_idx]
        else:
            stat_exog = None

        return (
            insample_y,
            insample_mask,
            outsample_y,
            outsample_mask,
            hist_exog,
            futr_exog,
            stat_exog,
        )

    def training_step(self, batch, batch_idx):
        # Create and normalize windows [Ws, L+H, C]
        batch = self._normalization(
            batch, val_size=self.val_size, test_size=self.test_size
        )
        windows = self._create_windows(batch, step="train")

        # Parse windows
        (
            insample_y,
            insample_mask,
            outsample_y,
            outsample_mask,
            hist_exog,
            futr_exog,
            stat_exog,
        ) = self._parse_windows(batch, windows)

        windows_batch = dict(
            insample_y=insample_y,  # [B, seq_len, 1]
            insample_mask=insample_mask,  # [B, seq_len, 1]
            futr_exog=futr_exog,  # [B, F, seq_len, 1+H]
            hist_exog=hist_exog,  # [B, C, seq_len]
            stat_exog=stat_exog,
        )  # [B, S]

        # Model predictions
        output = self(windows_batch)  # tuple([B, seq_len, H, output])
        if self.loss.is_distribution_output:
            outsample_y, y_loc, y_scale = self._inv_normalization(
                y_hat=outsample_y,
                temporal_cols=batch["temporal_cols"],
                y_idx=batch["y_idx"],
            )
            B = output[0].size()[0]
            T = output[0].size()[1]
            H = output[0].size()[2]
            output = [arg.view(-1, *(arg.size()[2:])) for arg in output]
            outsample_y = outsample_y.view(B * T, H)
            outsample_mask = outsample_mask.view(B * T, H)
            y_loc = y_loc.repeat_interleave(repeats=T, dim=0).squeeze(-1)
            y_scale = y_scale.repeat_interleave(repeats=T, dim=0).squeeze(-1)
            distr_args = self.loss.scale_decouple(
                output=output, loc=y_loc, scale=y_scale
            )
            loss = self.loss(y=outsample_y, distr_args=distr_args, mask=outsample_mask)
        else:
            loss = self.loss(y=outsample_y, y_hat=output, mask=outsample_mask)

        if torch.isnan(loss):
            print("Model Parameters", self.hparams)
            print("insample_y", torch.isnan(insample_y).sum())
            print("outsample_y", torch.isnan(outsample_y).sum())
            print("output", torch.isnan(output).sum())
            raise Exception("Loss is NaN, training stopped.")

        self.log(
            "train_loss",
            loss.detach().item(),
            batch_size=outsample_y.size(0),
            prog_bar=True,
            on_epoch=True,
        )
        self.train_trajectories.append((self.global_step, loss.detach().item()))
        return loss

    def validation_step(self, batch, batch_idx):
        if self.val_size == 0:
            return np.nan

        # Create and normalize windows [Ws, L+H, C]
        batch = self._normalization(
            batch, val_size=self.val_size, test_size=self.test_size
        )
        windows = self._create_windows(batch, step="val")
        y_idx = batch["y_idx"]

        # Parse windows
        (
            insample_y,
            insample_mask,
            outsample_y,
            outsample_mask,
            hist_exog,
            futr_exog,
            stat_exog,
        ) = self._parse_windows(batch, windows)

        windows_batch = dict(
            insample_y=insample_y,  # [B, seq_len, 1]
            insample_mask=insample_mask,  # [B, seq_len, 1]
            futr_exog=futr_exog,  # [B, F, seq_len, 1+H]
            hist_exog=hist_exog,  # [B, C, seq_len]
            stat_exog=stat_exog,
        )  # [B, S]

        # Remove train y_hat (+1 and -1 for padded last window with zeros)
        # tuple([B, seq_len, H, output]) -> tuple([B, validation_size, H, output])
        val_windows = (self.val_size) + 1
        outsample_y = outsample_y[:, -val_windows:-1, :]
        outsample_mask = outsample_mask[:, -val_windows:-1, :]

        # Model predictions
        output = self(windows_batch)  # tuple([B, seq_len, H, output])
        if self.loss.is_distribution_output:
            output = [arg[:, -val_windows:-1] for arg in output]
            outsample_y, y_loc, y_scale = self._inv_normalization(
                y_hat=outsample_y, temporal_cols=batch["temporal_cols"], y_idx=y_idx
            )
            B = output[0].size()[0]
            T = output[0].size()[1]
            H = output[0].size()[2]
            output = [arg.reshape(-1, *(arg.size()[2:])) for arg in output]
            outsample_y = outsample_y.reshape(B * T, H)
            outsample_mask = outsample_mask.reshape(B * T, H)
            y_loc = y_loc.repeat_interleave(repeats=T, dim=0).squeeze(-1)
            y_scale = y_scale.repeat_interleave(repeats=T, dim=0).squeeze(-1)
            distr_args = self.loss.scale_decouple(
                output=output, loc=y_loc, scale=y_scale
            )
            _, sample_mean, quants = self.loss.sample(distr_args=distr_args)

            if str(type(self.valid_loss)) in [
                "<class 'neuralforecast.losses.pytorch.sCRPS'>",
                "<class 'neuralforecast.losses.pytorch.MQLoss'>",
            ]:
                output = quants
            elif str(type(self.valid_loss)) in [
                "<class 'neuralforecast.losses.pytorch.relMSE'>"
            ]:
                output = torch.unsqueeze(sample_mean, dim=-1)  # [N,H,1] -> [N,H]

        else:
            output = output[:, -val_windows:-1, :]

        # Validation Loss evaluation
        if self.valid_loss.is_distribution_output:
            valid_loss = self.valid_loss(
                y=outsample_y, distr_args=distr_args, mask=outsample_mask
            )
        else:
            outsample_y, _, _ = self._inv_normalization(
                y_hat=outsample_y, temporal_cols=batch["temporal_cols"], y_idx=y_idx
            )
            output, _, _ = self._inv_normalization(
                y_hat=output, temporal_cols=batch["temporal_cols"], y_idx=y_idx
            )
            valid_loss = self.valid_loss(
                y=outsample_y, y_hat=output, mask=outsample_mask
            )

        if torch.isnan(valid_loss):
            raise Exception("Loss is NaN, training stopped.")

        self.log(
            "valid_loss",
            valid_loss.detach().item(),
            batch_size=outsample_y.size(0),
            prog_bar=True,
            on_epoch=True,
        )
        self.validation_step_outputs.append(valid_loss)
        return valid_loss

    def predict_step(self, batch, batch_idx):
        # Create and normalize windows [Ws, L+H, C]
        batch = self._normalization(batch, val_size=0, test_size=self.test_size)
        windows = self._create_windows(batch, step="predict")
        y_idx = batch["y_idx"]

        # Parse windows
        insample_y, insample_mask, _, _, hist_exog, futr_exog, stat_exog = (
            self._parse_windows(batch, windows)
        )

        windows_batch = dict(
            insample_y=insample_y,  # [B, seq_len, 1]
            insample_mask=insample_mask,  # [B, seq_len, 1]
            futr_exog=futr_exog,  # [B, F, seq_len, 1+H]
            hist_exog=hist_exog,  # [B, C, seq_len]
            stat_exog=stat_exog,
        )  # [B, S]

        # Model Predictions
        output = self(windows_batch)  # tuple([B, seq_len, H], ...)
        if self.loss.is_distribution_output:
            _, y_loc, y_scale = self._inv_normalization(
                y_hat=output[0], temporal_cols=batch["temporal_cols"], y_idx=y_idx
            )
            B = output[0].size()[0]
            T = output[0].size()[1]
            H = output[0].size()[2]
            output = [arg.reshape(-1, *(arg.size()[2:])) for arg in output]
            y_loc = y_loc.repeat_interleave(repeats=T, dim=0).squeeze(-1)
            y_scale = y_scale.repeat_interleave(repeats=T, dim=0).squeeze(-1)
            distr_args = self.loss.scale_decouple(
                output=output, loc=y_loc, scale=y_scale
            )
            _, sample_mean, quants = self.loss.sample(distr_args=distr_args)
            y_hat = torch.concat((sample_mean, quants), axis=2)
            y_hat = y_hat.view(B, T, H, -1)

            if self.loss.return_params:
                distr_args = torch.stack(distr_args, dim=-1)
                distr_args = torch.reshape(distr_args, (B, T, H, -1))
                y_hat = torch.concat((y_hat, distr_args), axis=3)
        else:
            y_hat, _, _ = self._inv_normalization(
                y_hat=output, temporal_cols=batch["temporal_cols"], y_idx=y_idx
            )
        return y_hat

    def fit(
        self,
        dataset,
        val_size=0,
        test_size=0,
        random_seed=None,
        distributed_config=None,
    ):
        """Fit.

        The `fit` method, optimizes the neural network's weights using the
        initialization parameters (`learning_rate`, `batch_size`, ...)
        and the `loss` function as defined during the initialization.
        Within `fit` we use a PyTorch Lightning `Trainer` that
        inherits the initialization's `self.trainer_kwargs`, to customize
        its inputs, see [PL's trainer arguments](https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.trainer.trainer.Trainer.html?highlight=trainer).

        The method is designed to be compatible with SKLearn-like classes
        and in particular to be compatible with the StatsForecast library.

        By default the `model` is not saving training checkpoints to protect
        disk memory, to get them change `enable_checkpointing=True` in `__init__`.

        **Parameters:**<br>
        `dataset`: NeuralForecast's `TimeSeriesDataset`, see [documentation](https://nixtla.github.io/neuralforecast/tsdataset.html).<br>
        `val_size`: int, validation size for temporal cross-validation.<br>
        `test_size`: int, test size for temporal cross-validation.<br>
        `random_seed`: int=None, random_seed for pytorch initializer and numpy generators, overwrites model.__init__'s.<br>
        """
        return self._fit(
            dataset=dataset,
            batch_size=self.batch_size,
            valid_batch_size=self.valid_batch_size,
            val_size=val_size,
            test_size=test_size,
            random_seed=random_seed,
            distributed_config=distributed_config,
        )

    def predict(self, dataset, step_size=1, random_seed=None, **data_module_kwargs):
        """Predict.

        Neural network prediction with PL's `Trainer` execution of `predict_step`.

        **Parameters:**<br>
        `dataset`: NeuralForecast's `TimeSeriesDataset`, see [documentation](https://nixtla.github.io/neuralforecast/tsdataset.html).<br>
        `step_size`: int=1, Step size between each window.<br>
        `random_seed`: int=None, random_seed for pytorch initializer and numpy generators, overwrites model.__init__'s.<br>
        `**data_module_kwargs`: PL's TimeSeriesDataModule args, see [documentation](https://pytorch-lightning.readthedocs.io/en/1.6.1/extensions/datamodules.html#using-a-datamodule).
        """
        self._check_exog(dataset)
        self._restart_seed(random_seed)
        data_module_kwargs = (
            self._set_quantile_for_iqloss(**data_module_kwargs) | self.dataloader_kwargs
        )

        if step_size > 1:
            raise Exception("Recurrent models do not support step_size > 1")

        # fcsts (window, batch, h)
        # Protect when case of multiple gpu. PL does not support return preds with multiple gpu.
        pred_trainer_kwargs = self.trainer_kwargs.copy()
        if (pred_trainer_kwargs.get("accelerator", None) == "gpu") and (
            torch.cuda.device_count() > 1
        ):
            pred_trainer_kwargs["devices"] = [0]

        trainer = pl.Trainer(**pred_trainer_kwargs)

        datamodule = TimeSeriesDataModule(
            dataset=dataset,
            valid_batch_size=self.valid_batch_size,
            **data_module_kwargs,
        )
        fcsts = trainer.predict(self, datamodule=datamodule)
        if self.test_size > 0:
            # Remove warmup windows (from train and validation)
            # [N,T,H,output], avoid indexing last dim for univariate output compatibility
            fcsts = torch.vstack(
                [fcst[:, -(1 + self.test_size - self.h) :, :] for fcst in fcsts]
            )
            fcsts = fcsts.numpy().flatten()
            fcsts = fcsts.reshape(-1, len(self.loss.output_names))
        else:
            fcsts = torch.vstack([fcst[:, -1:, :] for fcst in fcsts]).numpy().flatten()
            fcsts = fcsts.reshape(-1, len(self.loss.output_names))
        return fcsts
