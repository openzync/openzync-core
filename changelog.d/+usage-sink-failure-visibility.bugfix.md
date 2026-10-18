Re-raise usage metering failures after logging instead of swallowing them, so a metering outage reaches the ARQ retry loop rather than silently dropping usage rows.
