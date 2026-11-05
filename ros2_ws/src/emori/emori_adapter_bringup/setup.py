import os
from glob import glob

from setuptools import find_packages, setup

package_name = "emori_adapter_bringup"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.py")),
        (os.path.join("share", package_name, "config"), glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Pokamind AB",
    maintainer_email="dev@pokamind.com",
    description="EMORI Slice-2 bringup (launch + params).",
    license="Apache-2.0",
    entry_points={"console_scripts": []},
)
