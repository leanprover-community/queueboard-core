let diff_stat = DataTable.type('diff_stat', {
  detect: function (data) { return false; },
  order: {
    pre: function (data) {
      // Input has the form
      // <span style="color:green">42</span>/<span style="color:red">13</span>,
      // we extract the tuple (42, 13) and compute their sum 42+13.
      let parts = data.split('</span>/<span', 2);
      return Number(parts[0].slice(parts[0].search(">") + 1)) + Number(parts[1].slice(parts[1].search(">") + 1, -7));
    }
  },
});
let formatted_relativedelta = DataTable.type('formatted_relativedelta', {
  detect: function (data) { return data.startsWith('<div style="display:none">'); },
  order: {
    pre: function (data) {
      let main = (data.split('</div>', 2))[0].slice('<div style="display:none">'.length);
      // If there is no input data, main is the empty string.
      if (!main.includes('-')) {
          return -1;
      }
      const [days, seconds, ...rest] = main.split('-');
      return 100000 * Number(days) + Number(seconds);
    }
  }
});
// A PR assignee is sorted as a string; except that the string "nobody"
// (i.e., a PR is unassigned) is sorted last.
let assignee = DataTable.type('assignee', {
  order: {
    pre: function (data) { return (data == 'nobody') ? "zzzzzzzzzz" : data; }
  },
});
let getIdx;
if (STANDARD) {
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string;
  // |show_approvals| is true iff this table contains a visible column of github approvals.
  getIdx = function(aliasOrIdx, show_approvals) {
    // Some tables show a column of all PR approvals right after the assignee.
    // In this case, later indices must be shifted by 1.
    let offset = show_approvals ? 1 : 0;
    switch (aliasOrIdx) {
      case "number":
        return 0;
      case "author":
        return 1;
      case "title":
        return 2;
      // idx = 3 means the PR description, which is a hidden field
      case "labels":
        return 4;
      case "diff":
        return 5;
      // idx = 6 is the list of modified files
      case "numberChangedFiles":
        return 7;
      case "numberComments":
        return 8;
      // idx = 9 means the handles of users who commented or reviewed this PR
      case "assignee":
        return 10;
      // The following column indices depend on whether a dashboard shows
      // the list of users who approved a PR.
      case "approvals":
        if (show_approvals) { return 11; }
        break;
      case "lastUpdate":
        return 11 + offset;
      case "lastStatusChange":
        return 12 + offset;
      case "totalTimeReview":
        return 13 + offset;
      default:
        return aliasOrIdx;
    };
  }
} else {
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string;
  // |show_approvals| is true iff this table contains a visible column of github approvals.
  getIdx = function (aliasOrIdx) {
    switch (aliasOrIdx) {
      case "number":
        return 0;
      case "author":
        return 1;
      case "title":
        return 2;
      case "fromFork":
        return 3;
      case "ciStatus":
        return 4;
      case "hasMergeConflict":
        return 5
      case "isBlocked":
        return 6;
      case "isReady":
        return 7;
      case "awaitingReview":
        return 8;
      case "missingTopicLabel":
        return 10;
      case "overallStatus":
        return 11;
      default:
        return aliasOrIdx;
    };
  }
}
$(document).ready( function () {
  // Parse the URL for any initial configuration settings.
  // Future: use this for deciding which table to apply the options to.
  let fragment = window.location.hash;
  const params = new URLSearchParams(document.location.search);
  const search_params = params.get("search");
  const pageLength = params.get("length") || 10;
  const sort_params = params.getAll("sort");
  // The configuration for initial sorting of tables, for tables with and without approvals.
  let sort_config = [];
  let sort_config_approvals = [];
  for (const config of sort_params) {
    if (!config.includes('-')) {
      console.log(`invalid value ${config} passed as sort parameter`);
      continue;
    }
    const [col, dir, ...rest] = config.split('-');
    if (dir != "asc" && dir != "desc") {
      console.log(`invalid sorting direction ${dir} passed as sorting configuration`);
      continue;
    }
    if (STANDARD) {
      sort_config.push([getIdx(col, false), dir]);
      sort_config_approvals.push([getIdx(col, true), dir]);
    } else {
      sort_config.push([getIdx(col), dir]);
    }
   }
  const options = {
    stateDuration: 0,
    pageLength: pageLength,
    "searching": true,
    order: sort_config,
  };
  if (STANDARD) {
    options.columnDefs = [{ type: 'diff_stat', targets: 5 }, { type: 'assignee', targets: 10 }, { visible: false, targets: [3, 6, 9] } ];
  } else {
    // originally commented out by omit_column_config
    // NB. This is brittle; keep in sync with other changes!
    // options.columnDefs = [{ type: 'diff_stat', targets: 5 }, { visible: false, targets: [3, 6, 9] } ];
  }
  if (params.has("search")) {
    options.search = {
        search: search_params
    };
  }
  $('table').each(function () {
    if (STANDARD) {
      const tableId = $(this).attr('id')
      let tableOptions = { ...options }
      const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
      tableOptions.order = show_approval ? sort_config_approvals : sort_config;
      $(this).DataTable(tableOptions);
    } else {
      const tableId = $(this).attr('id') || "";
      if (tableId.startsWith("t-")) {
        $(this).DataTable(options);
      }
    }
  });
});
