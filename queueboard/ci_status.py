from enum import Enum, auto

class CIStatus(Enum):
    # All build jobs pass (or are skipped).
    Pass = auto()
    # Some build job fails which is not "inessential" (see below).
    Fail = auto()
    # Some build job fails, but all failing jobs are (usually) spurious failures,
    # or related to defects in the infrastructure.
    # Unless a PR actively modifies such infrastructure, this is not a bug in the PR.
    FailInessential = auto()
    # CI is currently running
    Running = auto()
    # Missing data.
    Missing = auto()

    @staticmethod
    def from_string(s: str):
        return {
            "pass": CIStatus.Pass,
            "fail": CIStatus.Fail,
            "fail-inessential": CIStatus.FailInessential,
            "running": CIStatus.Running,
            None: CIStatus.Missing,
        }[s]
