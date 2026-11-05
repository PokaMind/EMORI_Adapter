# One record of the motion arbiter's bounded trace, the same fields GET /api/move/trace serves.
# Times are the host's monotonic clock in seconds.
uint64 seq
float64 t
string event            # what happened to the move, as the arbiter names it
string move_id
string kind
string client
string lane
string reason           # carries the plan id and trace tag
string error_code
uint32 queued           # moves queued when the record was written
