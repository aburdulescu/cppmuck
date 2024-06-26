#!/usr/bin/env python

from setuptools import setup

setup(
    name="cppmuck",
    version="0.1.0",
    description="Generate C++ muck(mocks/stubs) needed for tests.",
    packages=["cppmuck"],
    install_requires=[
        "libclang==18.1.1",
    ],
    entry_points={
        "console_scripts": [
            "cppmuck=cppmuck.cppmuck:main",
        ]
    },
)
