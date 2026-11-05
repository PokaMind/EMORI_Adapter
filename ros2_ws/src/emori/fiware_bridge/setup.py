from setuptools import find_packages, setup

package_name = "fiware_bridge"

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
    description="ROS 2 -> FIWARE NGSI-LD bridge.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "bridge_node = fiware_bridge.bridge_node:main",
        ],
    },
)
