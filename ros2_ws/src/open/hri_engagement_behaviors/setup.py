from setuptools import find_packages, setup

package_name = "hri_engagement_behaviors"

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
    description="Engagement-to-behaviour mapping: drives Reachy Mini from ROS4HRI signals.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "behavior_node = hri_engagement_behaviors.behavior_node:main",
            # `skill_facade` moved to its own ament package, `emori_skill_facade`: the
            # only node allowed to turn a decision into motion is worth publishing (and
            # replacing, for another transport) on its own.
            "pyhri_demo = hri_engagement_behaviors.pyhri_engagement_demo:main",
        ],
    },
)
