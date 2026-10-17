select event_type, count(*) as event_count, sum(amount) as total_amount
from {{ source('raw', 'demo_events') }}
group by event_type
