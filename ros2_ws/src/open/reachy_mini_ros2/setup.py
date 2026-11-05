import os
from glob import glob

from setuptools import find_packages, setup

package_name = "reachy_mini_ros2"

setup(
    name=package_name,
    version="0.1.0",
    # `find_packages` and not a hand-written list, deliberately: an ament_python package
    # that forgets a SUBpackage (`reachy_mini_ros2.gaze`) installs an importable parent
    # and then raises ImportError at node start on the robot -- a failure that looks
    # exactly like a slow boot. `tests/test_ros_contracts.py` runs this same call and
    # asserts every subpackage on disk comes out of it.
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Pokamind AB",
    maintainer_email="dev@pokamind.com",
    description="The Reachy Mini as a native ROS 2 node: Pollen's transport-free "
                "Backend owned in-process, a motion arbiter with lanes and preemption, "
                "and a REST shim that keeps the daemon's dialect alive on :8000.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "robot_node = reachy_mini_ros2.robot_node:main",
        ],
    },
)
