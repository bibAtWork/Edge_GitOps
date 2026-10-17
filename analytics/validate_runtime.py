"""Validate the installed runtime and deployed Dagster launcher configuration."""
import os
from pathlib import Path
import subprocess
import yaml
from dagster import Definitions
from dagster._config import process_config, resolve_to_config_type
from dagster_k8s import K8sRunLauncher
from definitions import defs

Definitions.validate_loadable(defs)
config = yaml.safe_load(Path('/checks/dagster.yaml').read_text())
result = process_config(resolve_to_config_type(K8sRunLauncher.config_type()), config['run_launcher']['config'])
assert result.success, result.errors
os.environ['TRINO_DBT_PASSWORD'] = 'synthetic-offline-validation'
subprocess.run(['dbt', 'parse', '--project-dir', '/opt/analytics/dbt', '--profiles-dir', '/opt/analytics/dbt'], check=True)
