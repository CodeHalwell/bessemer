-- wrk2 passes its corrected HdrHistogram to done(). Values are microseconds.
function done(summary, latency, requests)
   io.write(string.format(
      "QUALIFICATION_PERCENTILES_US %.0f %.0f %.0f %.0f\n",
      latency:percentile(50), latency:percentile(95),
      latency:percentile(99), latency:percentile(99.9)))
end
