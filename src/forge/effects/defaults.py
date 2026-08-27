"""Default durable effect runtime wiring."""

from forge.effects.executors import EffectExecutorRegistry
from forge.effects.jira import register_jira_executors
from forge.effects.journal import RedisEffectJournal
from forge.effects.service import EffectService


def create_default_effect_service() -> EffectService:
    registry = EffectExecutorRegistry()
    register_jira_executors(registry)
    return EffectService(RedisEffectJournal(), registry)
