from __future__ import annotations

from app import models  # noqa: F401
from app.db import Base, engine


def main() -> None:
    """Create the disposable local schema used by the Web creator preview."""
    Base.metadata.create_all(bind=engine)
    print("Local Web API database is ready.")


if __name__ == "__main__":
    main()
