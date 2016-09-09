#!/usr/bin/python3

from celeriac import celeriac_version
from setuptools import setup


def get_requirements():
    with open('requirements.txt') as fd:
        return fd.read().splitlines()


setup(
    name='celeriac',
    version=celeriac_version,
    packages=['celeriac', 'celeriac.storage'],
    install_requires=get_requirements(),
    author='Fridolin Pokorny',
    author_email='fpokorny@redhat.com',
    maintainer='Fridolin Pokorny',
    maintainer_email='fpokorny@redhat.com',
    description='task flow management for Celery',
    url='https://github.com/fridex/celeriac',
    license='GPL',
    keywords='celery parsley yaml condition flow',
)
