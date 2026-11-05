from setuptools import find_packages, setup

package_name = "emori_hub"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/config", ["config/graph.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Pokamind AB",
    maintainer_email="dev@pokamind.com",
    description="The hub: the host application hosted beside rclpy, with its robot client "
                "over the native Reachy Mini node's services.",
    license="Apache-2.0",
)
