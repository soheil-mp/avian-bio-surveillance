import os
import sys

# Point to the source code
sys.path.insert(0, os.path.abspath('../../src'))

# Project information
project = 'Avian Biosurveillance'
copyright = '2026, Soheil'
author = 'Soheil'
release = '0.1.0'

# Extensions (all ship with Sphinx; no extra dependencies required)
extensions = [
    'sphinx.ext.autodoc',       # Automatic documentation from docstrings
    'sphinx.ext.autosummary',   # Summary tables for modules and classes
    'sphinx.ext.viewcode',      # Add links to highlighted source code
    'sphinx.ext.napoleon',      # Support for Google and NumPy style docstrings
    'sphinx.ext.intersphinx',   # Cross-reference external project documentation
    'sphinx.ext.todo',          # Support for todo items
]

# Autodoc / autosummary behaviour
autosummary_generate = True
autodoc_typehints = 'description'
autodoc_default_options = {
    'members': True,
    'undoc-members': True,
    'show-inheritance': True,
}

# Document the code from its docstrings without importing the heavy ML/audio/
# infrastructure stack. Without this, autodoc imports every module (TensorFlow,
# PyTorch, librosa, BirdNET, Dagster, ...), which makes the build extremely slow.
autodoc_mock_imports = [
    'torch', 'torchaudio', 'torchvision',
    'tensorflow', 'tensorflow_hub', 'tf_keras', 'keras',
    'transformers', 'huggingface_hub',
    'birdnet', 'birdnetlib', 'perch', 'perch_hoplite', 'chirp',
    'librosa', 'numba', 'llvmlite', 'resampy', 'soundfile', 'audioread',
    'dagster', 'dagster_graphql',
    'minio', 'boto3', 'botocore',
    'sqlalchemy', 'psycopg', 'psycopg2',
    'geopandas', 'shapely', 'pyproj', 'rasterio', 'rioxarray', 'fiona',
    'xarray', 'netCDF4', 'cfgrib', 'cdsapi', 'ee',
]

# Cross-references to external project documentation
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'numpy': ('https://numpy.org/doc/stable/', None),
    'pandas': ('https://pandas.pydata.org/docs/', None),
    'sklearn': ('https://scikit-learn.org/stable/', None),
}

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# Theme
html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']
