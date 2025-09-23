from enum import StrEnum

class CIStatus(StrEnum):
    # All build jobs pass (or are skipped).
    Pass = "pass"
    # Some build job fails which is not "inessential" (see below).
    Fail = "fail"
    # Some build job fails, but all failing jobs are (usually) spurious failures,
    # or related to defects in the infrastructure.
    # Unless a PR actively modifies such infrastructure, this is not a bug in the PR.
    FailInessential = "fail-inessential"
    # CI is currently running
    Running = "running"
    # Missing data.
    Missing = None

    @staticmethod
    def from_string(s: str):
        return {
            "pass": CIStatus.Pass,
            "fail": CIStatus.Fail,
            "fail-inessential": CIStatus.FailInessential,
            "running": CIStatus.Running,
            None: CIStatus.Missing,
        }[s]
