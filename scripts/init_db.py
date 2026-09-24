import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base, engine
from app import models  # noqa: F401


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print("DATABASE_INITIALIZED")
