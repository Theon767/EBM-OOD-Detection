from collections import defaultdict

import torch
from tqdm import tqdm
import pytorch_lightning as pl
import torch.nn.functional as F
from pytorch_lightning.core.decorators import auto_move_data

from uncertainty_est.archs.arch_factory import get_arch
from uncertainty_est.models.JEM.model import F, ConditionalF
from uncertainty_est.models.JEM.utils import (
    KHotCrossEntropyLoss,
    smooth_one_hot,
    init_random,
)


class JEM(pl.LightningModule):
    def __init__(
        self,
        arch_name,
        arch_config,
        learning_rate,
        momentum,
        weight_decay,
        buffer_size,
        n_classes,
        data_shape,
        smoothing,
        pyxce,
        pxsgld,
        pxysgld,
        class_cond_p_x_sample,
        sgld_batch_size,
        sgld_lr,
        sgld_std,
        reinit_freq,
        uncond,
        sgld_steps=20,
        energy_reg_weight=0.0,
        energy_reg_type="2",
    ):
        super().__init__()
        self.__dict__.update(locals())
        self.save_hyperparameters()

        arch = get_arch(arch_name, arch_config)
        self.model = (
            F(arch, n_classes) if self.uncond else ConditionalF(arch, n_classes)
        )

        if not self.uncond:
            assert (
                self.buffer_size % self.n_classes == 0
            ), "Buffer size must be divisible by args.n_classes"

        self.replay_buffer = init_random(self.buffer_size, data_shape).cpu()

    def forward(self, x):
        return self.model.classify(x)

    def compute_losses(self, x_lab, y_lab, x_p_d, dist, logits=None):
        l_pyxce, l_pxsgld, l_pxysgld = 0.0, 0.0, 0.0

        # log p(y|x) cross entropy loss
        if self.pyxce > 0:
            if logits is None:
                logits = self.model.classify(x_lab)
            l_pyxce = KHotCrossEntropyLoss()(logits, dist)
            l_pyxce *= self.pyxce

        # log p(x) using sgld
        if self.pxsgld > 0:
            if self.class_cond_p_x_sample:
                assert (
                    not self.uncond
                ), "can only draw class-conditional samples if EBM is class-cond"
                y_q = torch.randint(0, self.n_classes, (self.sgld_batch_size,)).to(
                    self.device
                )
                x_q = self.sample_q(self.replay_buffer, y=y_q)
            else:
                x_q = self.sample_q(
                    self.replay_buffer, n_steps=self.sgld_steps
                )  # sample from log-sumexp

            fp = self.model(x_p_d).mean()
            fq = self.model(x_q).mean()
            l_pxsgld = -(fp - fq)
            l_pxsgld *= self.pxsgld

        # log p(x|y) using sgld
        if self.pxysgld > 0:
            x_q_lab = self.sample_q(self.replay_buffer, y=y_lab)
            fp, fq = self.model(x_lab).mean(), self.model(x_q_lab).mean()
            l_pxysgld = -(fp - fq)
            l_pxysgld *= self.pxysgld

        l_pyxce += self.energy_reg_weight * self.compute_energy_reg(fp)

        return l_pyxce, l_pxsgld, l_pxysgld

    def training_step(self, batch, batch_idx):
        (x_lab, y_lab), (x_p_d, _) = batch
        dist = smooth_one_hot(y_lab, self.n_classes, self.smoothing)
        loss = sum(self.compute_losses(x_lab, y_lab, x_p_d, dist))
        return loss

    def validation_step(self, batch, batch_idx):
        (x_lab, y_lab), (x_p_d, _) = batch
        dist = smooth_one_hot(y_lab, self.n_classes, self.smoothing)
        logits = self(x_lab)
        torch.set_grad_enabled(True)
        loss = sum(self.compute_losses(x_lab, y_lab, x_p_d, dist, logits=logits))
        torch.set_grad_enabled(False)
        self.log("val_loss", loss)

        acc = (y_lab == logits.argmax(1)).float().mean(0).item()
        self.log("val_acc", acc)

    def test_step(self, batch, batch_idx):
        (x, y), (_, _) = batch
        y_hat = self(x)

        acc = (y == y_hat.argmax(1)).float().mean(0).item()
        self.log("test_acc", acc)

    def configure_optimizers(self):
        optim = torch.optim.AdamW(
            self.parameters(),
            betas=(self.momentum, 0.999),
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        scheduler = torch.optim.lr_scheduler.StepLR(optim, step_size=30, gamma=0.5)
        return [optim], [scheduler]

    def compute_energy_reg(self, energy):
        return torch.linalg.norm(energy, dim=-1, ord=self.energy_reg_type).mean()

    def get_gt_preds(self, loader):
        self.eval()
        torch.set_grad_enabled(False)
        gt, preds = [], []
        for x, y in tqdm(loader):
            x = x.to(self.device)
            y_hat = self(x).cpu()
            gt.append(y)
            preds.append(y_hat)
        return torch.cat(gt), torch.cat(preds)

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

    def sample_p_0(self, replay_buffer, bs, y=None):
        if len(replay_buffer) == 0:
            return init_random(bs, self.data_shape), []

        buffer_size = (
            len(replay_buffer) if y is None else len(replay_buffer) // self.n_classes
        )
        inds = torch.randint(0, buffer_size, (bs,))
        # if cond, convert inds to class conditional inds
        if y is not None:
            inds = y.cpu() * buffer_size + inds
            assert (
                not self.uncond
            ), "Can't drawn conditional samples without giving me y"

        buffer_samples = replay_buffer[inds].to(self.device)
        random_samples = init_random(bs, self.data_shape).to(self.device)
        choose_random = (torch.rand(bs) < self.reinit_freq).to(buffer_samples)[
            (...,) + (None,) * len(self.data_shape)
        ]
        samples = choose_random * random_samples + (1 - choose_random) * buffer_samples
        return samples.to(self.device), inds

    def sample_q(self, replay_buffer, y=None, n_steps=20, contrast=False):
        self.model.eval()
        bs = self.sgld_batch_size if y is None else y.size(0)

        # generate initial samples and buffer inds of those samples (if buffer is used)
        init_sample, buffer_inds = self.sample_p_0(replay_buffer, bs=bs, y=y)
        x_k = init_sample.clone()
        x_k.requires_grad = True

        # sgld
        for _ in range(n_steps):
            if not contrast:
                energy = self.model(x_k, y=y).sum()
            else:
                if y is not None:
                    dist = smooth_one_hot(y, self.n_classes, self.smoothing)
                else:
                    dist = torch.ones((bs, self.n_classes)).to(self.device)
                output, target, _, _ = self.model.joint(
                    img=x_k, dist=dist, evaluation=True
                )
                energy = -1.0 * F.cross_entropy(output, target)
            f_prime = torch.autograd.grad(energy, [x_k], retain_graph=True)[0]
            x_k.data += self.sgld_lr * f_prime + self.sgld_std * torch.randn_like(x_k)
        self.model.train()
        final_samples = x_k.detach()

        # update replay buffer
        if len(replay_buffer) > 0:
            replay_buffer[buffer_inds] = final_samples.cpu()
        return final_samples