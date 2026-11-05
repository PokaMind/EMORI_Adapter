# The installed add-on, if there is one. Latched on /emori/addon/status; published on change
# and polled at 2 Hz. `status_json` is the add-on's own status() verbatim, which is
# content-free by the add-on API's contract.
std_msgs/Header header
bool installed
string name
uint32 api_version
bool busy
bool selection_reserved
string mode
string status_json          # status() verbatim, already content-free
