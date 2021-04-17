# Fix https://github.com/pytorch/pytorch/issues/37377
import numpy as _

import os
import sys
from uuid import uuid4

sys.path.insert(0, os.getcwd())

from pathlib import Path
from datetime import datetime

import yaml
import seml
from sacred import Experiment
import pytorch_lightning as pl

from uncertainty_est.models import MODELS
from uncertainty_est.data.dataloaders import get_dataloader


ex = Experiment()
seml.setup_logger(ex)


@ex.post_run_hook
def collect_stats(_run):
    seml.collect_exp_stats(_run)


@ex.config
def config():
    overwrite = None
    db_collection = None
    if db_collection is not None:
        ex.observers.append(
            seml.create_mongodb_observer(db_collection, overwrite=overwrite)
        )


@ex.main
def run(
    trainer_config,
    model_name,
    model_config,
    dataset,
    seed,
    batch_size,
    ood_dataset,
    earlystop_config,
    checkpoint_config,
    data_shape,
    _run,
    sigma=0.0,
    output_folder=None,
    log_dir="logs",
    num_workers=4,
    test_ood_datasets=[],
    mutation_rate=0.0,
    **kwargs,
):
    pl.seed_everything(seed)

    test_ood_dataloaders = []
    for test_ood_dataset in test_ood_datasets:
        loader = get_dataloader(
            test_ood_dataset,
            "test",
            batch_size,
            data_shape=data_shape,
            sigma=sigma,
            ood_dataset=None,
            num_workers=num_workers,
        )
        test_ood_dataloaders.append((test_ood_dataset, loader))

    model = MODELS[model_name](
        **model_config, test_ood_dataloaders=test_ood_dataloaders
    )

    train_loader = get_dataloader(
        dataset,
        "train",
        batch_size,
        data_shape=data_shape,
        ood_dataset=ood_dataset,
        sigma=sigma,
        num_workers=num_workers,
        mutation_rate=mutation_rate,
    )
    val_loader = get_dataloader(
        dataset,
        "val",
        batch_size,
        data_shape=data_shape,
        sigma=sigma,
        ood_dataset=ood_dataset,
        num_workers=num_workers,
    )
    test_loader = get_dataloader(
        dataset,
        "test",
        batch_size,
        data_shape=data_shape,
        sigma=sigma,
        ood_dataset=None,
        num_workers=num_workers,
    )

    output_folder = (
        f'{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}_{uuid4()}'
        if output_folder is None
        else output_folder
    )
    out_path = Path(log_dir) / model_name / dataset / output_folder
    out_path.mkdir(exist_ok=False, parents=True)

    with (out_path / "config.yaml").open("w") as f:
        f.write(yaml.dump(_run.config))

    callbacks = []
    callbacks.append(
        pl.callbacks.ModelCheckpoint(dirpath=out_path, **checkpoint_config)
    )
    if earlystop_config is not None:
        es_callback = pl.callbacks.EarlyStopping(**earlystop_config)
        callbacks.append(es_callback)

    logger = pl.loggers.TensorBoardLogger(out_path, default_hp_metric=False)

    trainer = pl.Trainer(
        **trainer_config,
        logger=logger,
        callbacks=callbacks,
        progress_bar_refresh_rate=100,
    )
    trainer.fit(model, train_loader, val_loader)

    try:
        result = trainer.test(test_dataloaders=test_loader)
    except:
        result = trainer.test(test_dataloaders=test_loader, ckpt_path=None)

    logger.log_hyperparams(model.hparams, result[0])

    return result[0]


if __name__ == "__main__":
    ex.run_commandline()
