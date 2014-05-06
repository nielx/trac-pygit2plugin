#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys

from setuptools import setup, find_packages
from pkg_resources import parse_version

extra = {}

setup(
    name='TracPygit2Plugin',
    version='0.12.0.1',
    description='Pygit2 integration for Git repository on Trac 0.12+',
    license='BSD',  # the same as Trac
    url='http://trac-hacks.org/wiki/TracPygit2Plugin',
    author='Jun Omae',
    author_email='jun66j5@gmail.com',
    packages=find_packages(exclude=['*.tests*']),
    package_data={},
    test_suite='tracext.pygit2.tests.suite',
    zip_safe=True,
    install_requires=['Trac', 'pygit2'],
    entry_points={
        'trac.plugins': [
            'tracext.pygit2.git_fs = tracext.pygit2.git_fs',
        ],
    },
    **extra)
