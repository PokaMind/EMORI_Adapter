cmake_minimum_required(VERSION 3.8)
project(emori_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(action_msgs REQUIRED)

# Every .msg/.srv/.action file on disk must appear here: a generated package silently
# omits an interface nobody listed, and the ImportError only shows up at node start
# on the robot.  tests/test_ros_contracts.py pins this list against the directory.
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Context.msg"
  "msg/SpeechCue.msg"
  "msg/SetExpression.msg"
  "action/LookAt.action"
  "action/ReplayMotion.action"
  "msg/SessionState.msg"
  "msg/SessionEvent.msg"
  "msg/GestureEvent.msg"
  "msg/GestureState.msg"
  "msg/VoiceState.msg"
  "msg/PlayerState.msg"
  "msg/MotionIntent.msg"
  "msg/MotionPlanEvent.msg"
  "msg/AddonStatus.msg"
  "msg/KioskLanguage.msg"
  "srv/SessionCommand.srv"
  "srv/StartSession.srv"
  "srv/SetLanguage.srv"
  "srv/Recognize.srv"
  "srv/SubmitMotionBatch.srv"
  "srv/GetHubStatus.srv"
  "srv/GetVoiceMenu.srv"
  "srv/SetTtOverrides.srv"
  "action/Say.action"
  DEPENDENCIES std_msgs builtin_interfaces action_msgs
)

ament_package()
