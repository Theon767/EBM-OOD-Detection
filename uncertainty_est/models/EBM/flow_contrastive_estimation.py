import math
from collections import defaultdict

import torch
from tqdm import tqdm
import pytorch_lightning as pl
import torch.nn.functional as F
from torch import distributions
import matplotlib.pyplot as plt

from uncertainty_est.archs.arch_factory import get_arch
from uncertainty_est.models.ood_detection_model import OODDetectionModel
from uncertainty_est.utils.utils import (
    to_np,
    estimate_normalizing_constant,
    sum_except_batch,
)


class FlowContrastiveEstimation(OODDetectionModel):
    """Implementation of Noise Contrastive Estimation http://proceedings.mlr.press/v9/gutmann10a.html"""

    def __init__(
        self,
        arch_name,
        arch_config,
        flow_arch_name,
        flow_arch_config,
        learning_rate,
        momentum,
        weight_decay,
        flow_learning_rate,
        rho=0.5,
        is_toy_dataset=False,
        toy_dataset_dim=2,
        test_ood_dataloaders=[],
    ):
        super().__init__(test_ood_dataloaders)
        self.automatic_optimization = False
        self.__dict__.update(locals())
        self.save_hyperparameters()

        self.model = get_arch(arch_name, arch_config)
        self.noise_dist = get_arch(flow_arch_name, flow_arch_config)

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx, optimizer_idx):
        optim_ebm, optim_flow = self.optimizers()
        x, _ = batch

        noise, neg_f_log_p = self.noise_dist.sample(len(x))
        neg_e_log_p = self.model(noise.detach()).logsumexp(-1)

        target = torch.zeros_like(neg_e_log_p).long().to(self.device)
        neg_pred = torch.stack(
            (neg_f_log_p + math.log(1 - self.rho), neg_e_log_p + math.log(self.rho)), -1
        )
        neg_loss = F.cross_entropy(neg_pred, target)

        pos_f_log_p = self.noise_dist.log_prob(x)
        pos_e_log_p = self.model(x).logsumexp(-1)

        target = torch.ones_like(pos_e_log_p).long().to(self.device)
        pos_pred = torch.stack(
            (pos_f_log_p + math.log(1 - self.rho), pos_e_log_p + math.log(self.rho)), -1
        )
        pos_loss = F.cross_entropy(pos_pred, target)

        loss = self.rho * pos_loss + (1 - self.rho) * neg_loss

        acc = (
            torch.cat((pos_e_log_p > pos_f_log_p, neg_f_log_p > neg_e_log_p))
            .float()
            .mean()
        )
        self.log("train/acc", acc, prog_bar=True)

        if acc <= 0.55:
            optim_ebm.zero_grad()
            self.manual_backward(loss)
            optim_ebm.step()
            self.log("train/loss", loss, prog_bar=True)
        else:
            optim_flow.zero_grad()
            self.manual_backward(-loss)
            optim_flow.step()
            self.log("train/flow_loss", -loss, prog_bar=True)

    def validation_step(self, batch, batch_idx):
        return

    def validation_epoch_end(self, outputs):
        if self.is_toy_dataset and self.toy_dataset_dim == 2:
            interp = torch.linspace(-4, 4, 500)
            x, y = torch.meshgrid(interp, interp)
            data = torch.stack((x.reshape(-1), y.reshape(-1)), 1).to(self.device)
            p_xy = torch.exp(self(data))
            px = to_np(p_xy.sum(1))
            flow_px = to_np(self.noise_dist.log_prob(data).exp())

            x, y = to_np(x), to_np(y)
            for i in range(p_xy.shape[1]):
                fig, ax = plt.subplots()
                mesh = ax.pcolormesh(x, y, to_np(p_xy[:, i]).reshape(*x.shape))
                fig.colorbar(mesh)
                self.logger.experiment.add_figure(
                    f"dist/p(x,y={i})", fig, self.current_epoch
                )
                plt.close()

            fig, ax = plt.subplots()
            mesh = ax.pcolormesh(x, y, px.reshape(*x.shape))
            fig.colorbar(mesh)
            self.logger.experiment.add_figure("dist/p(x)", fig, self.current_epoch)
            plt.close()

            fig, ax = plt.subplots()
            mesh = ax.pcolormesh(x, y, flow_px.reshape(*x.shape))
            fig.colorbar(mesh)
            self.logger.experiment.add_figure("dist/Flow p(x)", fig, self.current_epoch)
            plt.close()

    def test_step(self, batch, batch_idx):
        self.to(torch.float32)
        x, y = batch
        y_hat = self.model(x)

        acc = (y == y_hat.argmax(1)).float().mean(0).item()
        self.log("test_acc", acc)
        return y_hat

    def test_epoch_end(self, logits):
        if self.is_toy_dataset:
            # Estimate normalizing constant Z by numerical integration
            log_Z = torch.log(
                estimate_normalizing_constant(
                    lambda x: self(x).exp().sum(1),
                    device=self.device,
                    dimensions=self.toy_dataset_dim,
                    dtype=torch.float32,
                )
            )
            self.log("log_Z", log_Z)

            logits = torch.cat(logits, 0)
            log_px = logits.logsumexp(1) - log_Z
            self.log("log_likelihood", log_px.mean())

        super().test_epoch_end()

    def configure_optimizers(self):
        optim = torch.optim.AdamW(
            self.model.parameters(),
            betas=(self.momentum, 0.999),
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        scheduler = torch.optim.lr_scheduler.StepLR(optim, step_size=30, gamma=0.5)

        optim_flow = torch.optim.AdamW(
            self.noise_dist.parameters(),
            betas=(self.momentum, 0.999),
            lr=self.flow_learning_rate,
            weight_decay=self.weight_decay,
        )
        scheduler_flow = torch.optim.lr_scheduler.StepLR(optim, step_size=30, gamma=0.5)

        return [optim, optim_flow], [scheduler, scheduler_flow]

    def ood_detect(self, loader):
        self.eval()
        torch.set_grad_enabled(False)
        scores = []
        for x, y in tqdm(loader):
            x = x.to(self.device)
            score = self.model(x).cpu()
            scores.append(score)

        uncert = {}
        uncert["p(x)"] = torch.cat(scores).cpu().numpy()
        return uncert
