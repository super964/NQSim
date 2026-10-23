"""日间 / 夜间主题配色。"""

from __future__ import annotations

DARK = {
    "name": "夜间模式",
    "bg": "#0b0e14",
    "panel": "#131722",
    "panel2": "#1a1f2c",
    "panel3": "#222834",
    "border": "#2b313d",
    "text": "#d7dae2",
    "dim": "#787b86",
    "faint": "#4a4e5a",
    "grid": "#1c212b",
    "up": "#26a69a",
    "down": "#ef5350",
    "up_fill": "#26a69a",
    "down_fill": "#ef5350",
    "accent": "#2962ff",
    "accent2": "#7b61ff",
    "bid": "#26a69a",
    "ask": "#ef5350",
    "equity": "#4c8dff",
    "dd": "#ef5350",
    "warn": "#f0b90b",
    "good": "#26a69a",
    "bad": "#ef5350",
    "flat": "#8b8f9c",
    "cross": "#6b7280",
    "vol_up": "#1d6f66",
    "vol_down": "#8a3230",
    "shadow": "#000000",
}

LIGHT = {
    "name": "日间模式",
    "bg": "#f2f4f8",
    "panel": "#ffffff",
    "panel2": "#f7f9fc",
    "panel3": "#eef1f7",
    "border": "#d7dde8",
    "text": "#1b2233",
    "dim": "#6b7385",
    "faint": "#9aa2b1",
    "grid": "#e7ebf2",
    "up": "#0f9b73",
    "down": "#d94b4b",
    "up_fill": "#0f9b73",
    "down_fill": "#d94b4b",
    "accent": "#2563eb",
    "accent2": "#6d43d8",
    "bid": "#0f9b73",
    "ask": "#d94b4b",
    "equity": "#2563eb",
    "dd": "#d94b4b",
    "warn": "#b45309",
    "good": "#0f9b73",
    "bad": "#d94b4b",
    "flat": "#7b8496",
    "cross": "#8b93a3",
    "vol_up": "#a5dfcd",
    "vol_down": "#f2b8b8",
    "shadow": "#000000",
}

THEMES = {"dark": DARK, "light": LIGHT}


def get_theme(mode: str) -> dict:
    return THEMES.get(mode, DARK)
