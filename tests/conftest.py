import os

# Проверки не должны загружать локальные ключи, в том числе при установленном tau2.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"


import pytest
from retail_data import sample_shop


@pytest.fixture
def snapshot():
    return sample_shop()
