"""Colors for signed trading values throughout the dashboard."""

POSITIVE = "#d64c59"
NEGATIVE = "#3584d5"
NEUTRAL = "#88939e"


def direction_color(value):
    if value > 0:
        return POSITIVE
    if value < 0:
        return NEGATIVE
    return None
