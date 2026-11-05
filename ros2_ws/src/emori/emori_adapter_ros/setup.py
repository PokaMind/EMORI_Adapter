from setuptools import find_packages, setup

package_name = "emori_adapter_ros"

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
    description="EMORI -> ROS4HRI adapter node.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "publisher_node = emori_adapter_ros.publisher_node:main",
        ],
    },
)
