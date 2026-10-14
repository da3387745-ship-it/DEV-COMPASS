"""Check the MongoDB connection configured in the project .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')


def main() -> int:
    """Ping MongoDB and return a process exit code without exposing credentials."""
    uri = os.getenv('MONGODB_URI', '').strip()
    if not uri:
        print('Connection failed: MONGODB_URI is not set in the project .env file.')
        return 1

    client = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000, appname='DevCompassConnectionCheck')
        client.admin.command('ping')
    except (PyMongoError, ValueError) as error:
        print(
            f'Connection failed ({type(error).__name__}). Check the Atlas URI, '
            'network access, and database user credentials.'
        )
        return 1
    finally:
        if client is not None:
            client.close()

    print('Connected successfully to MongoDB.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
