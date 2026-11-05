from setuptools import find_packages, setup

package_name = "emori_skill_native"

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
    description="ros_native: the skill facade's transport onto the native Reachy Mini "
                "ROS 2 node (GoTo / PlayRecordedMove actions, no HTTP hop).",
    license="Apache-2.0",
    entry_points={
        # How the facade finds this transport with no import of its own: the name in
        # EMORI_SKILL_TRANSPORT / the launch argument is looked up in this group. The
        # facade may not depend on rclpy or the adapter core, so the dependency has to
        # point this way round.
        "emori_skill_facade.transports": [
            "ros_native = emori_skill_native.native:make_transport",
        ],
    },
)
