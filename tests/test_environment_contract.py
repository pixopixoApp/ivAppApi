from types import SimpleNamespace

import pytest

from app.config import validate_environment_contract


def _settings(environment: str, database_url: str) -> SimpleNamespace:
    production = environment == "production"
    return SimpleNamespace(
        pixo_environment=environment,
        database_url=database_url,
        rds_host="rds.internal" if production else "",
        public_share_base_url=(
            "https://api.pixopixo.com" if production else "https://api.pixopixo.cn"
        ),
        public_game_base_url=(
            "https://www.pixopixo.com/"
            if production
            else "https://demo.pixopixo.cn/game/"
        ),
        aliyun_oss_public_base_url="https://video.pixopixo.cn",
    )


def test_development_contract_accepts_local_mysql() -> None:
    validate_environment_contract(
        _settings("development", "mysql+pymysql://user:pass@mysql:3306/ivapp")
    )


def test_development_contract_rejects_rds() -> None:
    with pytest.raises(RuntimeError, match="local Docker MySQL"):
        validate_environment_contract(
            _settings("development", "mysql+pymysql://user:pass@rds.internal:3306/ivapp")
        )


def test_production_contract_accepts_rds_and_temporary_media_origin() -> None:
    validate_environment_contract(
        _settings("production", "mysql+pymysql://user:pass@rds.internal:3306/ivapp")
    )


def test_production_contract_rejects_local_mysql() -> None:
    with pytest.raises(RuntimeError, match="private RDS endpoint"):
        validate_environment_contract(
            _settings("production", "mysql+pymysql://user:pass@mysql:3306/ivapp")
        )


def test_production_contract_rejects_a_different_remote_database() -> None:
    with pytest.raises(RuntimeError, match="private RDS endpoint"):
        validate_environment_contract(
            _settings("production", "mysql+pymysql://user:pass@other.internal:3306/ivapp")
        )
