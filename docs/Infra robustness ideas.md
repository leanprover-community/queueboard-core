## Robustness ideas for my infrastructure

data integrity check: awaiting-CI label but no running CI

data integrity check: do a deep comparison of the files
- all key data exposed to the webpage (perhaps generate a big table of everything twice and just diff things?)
- compare CI state (if possible)



- avoid push races/conflicts
  - unracying: fix the inherent race between the two workflows, as well as I can
  - make pushing more resilient: add automatic conflict resolution

- race condition/duplicate work: publishing job runs `check_data_integrity`, requesting the re-download of some PRs
I could happen, however, that this workflow just ran before the gather_stats workflow, and some of these PRs would get downloaded anyway, right? Then, this might lead to re-downloading twice.
mitigation: schedule those with completed running CI first, as these are more likely to get missed

- another race condition: both jobs write to "redownload.txt" (gather_stats.yml empties it when successful, publish-dashboard.yml writes new content); this could lead to a push race

- avoid web-hooks time-outs: make sure the more frequent polling doesn't break havoc
  - trigger these hooks often enough:
    whenever a force-push happens, CI finishes, a new commit is made, somebody comments, reviews or some label changes. Also for draft status changes;  cross-references are not important. (Compare the full list of change events.)
  - these hooks get added to a queue, right --- so no flooding of workers, I hope?!
  - are these batched? if one PR has a label removed, a comment made, and a commit pushed --- can we make it so the last request "within reason" wins? Or only new data is downloaded (somehow, without re-implementing everything)? Would be nice to not re-download the world *all the time*. The current polling frequency keeps this within reason, would be nice to not regress *too* hard.

- re-running CI jobs might also not be caught by the current "updates" logic; make sure web-hooks catch this

- make sure to detect all PR updates: web-hooks should help
hack for CI completed: auto-add awaiting-CI on every new push --- gets removed once sth passes :-)

- auto-identify out-of-date data
  - improve the data integrity check, to check old CI results: done
  - improve the data integrity check, to find closed PRs which are still marked open: done
  - improve data integrity check: make it detect force-pushes
  - switch to full paranoia mode: comparing the key data on all open PRs; *that* paranoia could suffice once a day or so (to only plug the remaining leaks) --- multi-layered approach
- automatically re-download out of date PRs: that works reasonably well. Remaining improvements are tracked separately.

- recover from time-outs or rate-limiting in re-downloading
   - avoid discarding up to two re-downloaded PRs if only one fails
   - avoid getting stuck in a loop of one PR blocking everything
    partial fix implement (shuffling PRs, so new PRs will still tend to this)
   => auto-classify re-downloaded PRs as stubborns also, perhaps after 5 attempts? delete once successful?

- try logic for stubborn PRs: remove broken data, re-queue and try again. That step should generally not fail.

small observations, might not be worth fixing yet
- if backfilling a "standard PR" fails, backfilling a stubborn PR is not attempted yet
=> could re-order these steps for bigger robustness
