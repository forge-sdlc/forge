"""Default durable effect runtime wiring."""

from forge.effects.executors import EffectExecutorRegistry
from forge.effects.jira import register_jira_executors
from forge.effects.journal import RedisEffectJournal
from forge.effects.repository import register_repository_executors
from forge.effects.service import EffectService
from forge.effects.source_control import register_source_control_executors


def create_default_effect_service() -> EffectService:
    registry = EffectExecutorRegistry()
    register_jira_executors(registry)
    register_source_control_executors(registry)
    register_repository_executors(registry)
    return EffectService(RedisEffectJournal(), registry)
