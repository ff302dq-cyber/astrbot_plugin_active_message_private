from __future__ import annotations

import sys
import types


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Filter:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: lambda function: function


class _Star:
    def __init__(self, context=None):
        self.context = context


class _Plain:
    def __init__(self, text):
        self.text = text


class _MessageChain:
    def __init__(self, chain):
        self.chain = chain


api = types.ModuleType("astrbot.api")
api.logger = _Logger()
api.AstrBotConfig = dict

event = types.ModuleType("astrbot.api.event")
event.AstrMessageEvent = object
event.filter = _Filter()

star = types.ModuleType("astrbot.api.star")
star.Context = object
star.Star = _Star
star.StarTools = type("StarTools", (), {})

all_api = types.ModuleType("astrbot.api.all")
all_api.Plain = _Plain
all_api.MessageChain = _MessageChain
all_api.__all__ = ["Plain", "MessageChain"]

astrbot = types.ModuleType("astrbot")
astrbot.api = api

sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)
sys.modules.setdefault("astrbot.api.event", event)
sys.modules.setdefault("astrbot.api.star", star)
sys.modules.setdefault("astrbot.api.all", all_api)
