from setuptools import setup, find_packages

setup(
    name="sweepstake-genie",
    version="0.1.0",
    description="Automated sweepstakes discovery and entry tool",
    author="Sweepstake Genie",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "playwright>=1.40.0",
        "beautifulsoup4>=4.12.0",
        "requests>=2.31.0",
        "pyyaml>=6.0",
        "rich>=13.0.0",
        "click>=8.1.0",
    ],
    entry_points={
        "console_scripts": [
            "sweepstake-genie=main:cli",
        ],
    },
)
