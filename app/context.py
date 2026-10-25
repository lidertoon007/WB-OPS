"""Кто выполняет текущий запрос (для журнала действий): «владелец» на этом компьютере или сотрудник команды."""
from contextvars import ContextVar

current_actor: ContextVar[str] = ContextVar("current_actor", default="")
