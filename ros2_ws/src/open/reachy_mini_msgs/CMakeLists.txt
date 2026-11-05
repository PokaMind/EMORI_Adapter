cmake_minimum_required(VERSION 3.8)
project(reachy_mini_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(action_msgs REQUIRED)

# Every .msg/.action/.srv file on disk must appear here: a generated package silently
# omits an interface nobody listed, and the ImportError only shows up at node start on
# the robot.  Same rule, same reason, as emori_msgs.
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/MotionStatus.msg"
  "msg/BodyTarget.msg"
  "action/GoTo.action"
  "action/PlayRecordedMove.action"
  "srv/SetMotorMode.srv"
  "srv/StopMove.srv"
  "srv/SetOwner.srv"
  "msg/PoseRpy.msg"
  "msg/Rpy.msg"
  "msg/MotionBlock.msg"
  "msg/FullState.msg"
  "msg/MoveTraceRecord.msg"
  "srv/GetFullState.srv"
  "srv/SubmitMove.srv"
  "srv/GetMoveTrace.srv"
  "srv/GetRobotStatus.srv"
  "srv/PlaySound.srv"
  DEPENDENCIES std_msgs geometry_msgs builtin_interfaces action_msgs
)

ament_package()
