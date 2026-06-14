"""ML thermal-wind classifier — Phase C ceiling spike.

This subpackage is the heart of the ml-classifier plan: it loads the
replay CSV (Phase A), fits the model families the spike benchmarks
(`oracle.ml.train`), and scores them against the rule baseline
(`oracle.ml.evaluate`). The CLI surface is in `oracle.cli:ml_app`.

Public API (stable for Phase D/E callers):
- `dataset.load_replay_csv`, `dataset.split_by_year`, `dataset.ReplayDataset`
- `train.fit_logistic`, `train.fit_hgb`, `train.fit_tabpfn`, `train.fit_all`
- `train.FittedClassifier` (with `.save` / `.load`)
- `evaluate.score_head_to_head`, `evaluate.format_text_report`,
  `evaluate.HeadToHeadResult`

Module-level lazy imports keep `oracle --help` working on the prod
images, which don't install the `ml` extra.
"""
from __future__ import annotations

# Intentionally empty: the subpackage's modules are imported on demand by
# the CLI subcommands (and by tests). Keeping `__init__.py` import-free
# means `import oracle.ml` doesn't require scikit-learn — matching the
# Phase B `importlib.util.find_spec` dep guard in `cli.py`.
