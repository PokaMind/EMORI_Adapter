from setuptools import find_packages, setup

package_name = "hri_context_analyzer"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Pokamind AB",
    maintainer_email="dev@pokamind.com",
    description="Context Analysis Module: publishes REP-155 /context topics.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "context_analyzer = hri_context_analyzer.context_analyzer_node:main",
        ],
    },
)
