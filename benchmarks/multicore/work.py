"""Picklable deterministic Python work shared by every benchmark backend."""


def checksum(iterations: int) -> int:
    value = 0
    for index in range(iterations):
        value = (value + index * index) % 1_000_003
    return value
