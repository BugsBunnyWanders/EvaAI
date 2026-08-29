from typing import cast

from fastapi import Request

from eva_ai.db import Database


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)
