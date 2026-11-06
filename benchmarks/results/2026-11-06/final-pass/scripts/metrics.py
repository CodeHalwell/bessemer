"""Calculate completed-response rates and correct archived result summaries."""

import math


def response_rates(oha):
    summary = oha["summary"]
    duration = summary["total"]
    responses = sum(oha["statusCodeDistribution"].values())
    if duration <= 0 or responses <= 0:
        raise ValueError("a measured run needs a duration and completed responses")
    attempts = summary["requestsPerSec"] * duration
    if not math.isclose(
        attempts, responses + sum(oha.get("errorDistribution", {}).values()), abs_tol=1
    ):
        raise ValueError("oha attempts do not match responses and errors")
    return {
        "attempt_req_s": summary["requestsPerSec"],
        "req_s": responses / duration,
        "ok_req_s": oha["statusCodeDistribution"].get("200", 0) / duration,
        "no_response": round(attempts) - responses,
        "total_responses": responses,
    }


def corrected_result(result, oha):
    rates = response_rates(oha)
    if result["total_responses"] != rates["total_responses"]:
        raise ValueError("result and oha response counts disagree")
    prior_rate = result["req_s"]
    recorded_rate = result.get("attempt_req_s", prior_rate)
    if not math.isclose(recorded_rate, rates["attempt_req_s"], rel_tol=1e-9):
        raise ValueError("result and oha attempt rates disagree")
    result = dict(result)
    result["ctx_switches_per_req"] *= prior_rate / rates["req_s"]
    result["us_cpu_per_req"] = result["server_cpus"] * 1e6 / rates["req_s"]
    result.update(rates)
    requests_in_window = result["req_s"] * result["window_s"]
    result["threads"] = {
        name: dict(thread, us_per_req=thread["cpu_s"] * 1e6 / requests_in_window)
        for name, thread in result.get("threads", {}).items()
    }
    return result
