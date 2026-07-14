"""Repository layer — all database access lives here.

Repositories are responsible **only** for persistence concerns (queries,
transactions, connection management).  They return domain ORM models and
must not contain business logic — that belongs in the service layer.

Every repository class accepts an ``AsyncSession`` in its constructor.
"""

from repositories.custom_instruction_repository import CustomInstructionRepository
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.oauth_repository import OAuthRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository

__all__: list[str] = [
    "CustomInstructionRepository",
    "EpisodeRepository",
    "FactRepository",
    "OAuthRepository",
    "SessionRepository",
    "UserRepository",
]
