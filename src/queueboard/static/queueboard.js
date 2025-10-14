const diff_stat = DataTable.type('diff_stat', {
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
const formatted_relativedelta = DataTable.type('formatted_relativedelta', {
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
const assignee = DataTable.type('assignee', {
  order: {
    pre: function (data) { return (data == 'nobody') ? "zzzzzzzzzz" : data; }
  },
});
let getIdx;
let getAlias;
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
  // an "inverse" function to getIdx
  getAlias = function(idx, show_approvals) {
    // Some tables show a column of all PR approvals right after the assignee.
    // In this case, later indices must be shifted by 1.
    switch (idx) {
      case 0:
        return "number";
      case 1:
        return "author";
      case 2:
        return "title";
      // idx = 3 means the PR description, which is a hidden field
      case 4:
        return "labels";
      case 5:
        return "diff";
      // idx = 6 is the list of modified files
      case 7:
        return "numberChangedFiles";
      case 8:
        return "numberComments";
      // idx = 9 means the handles of users who commented or reviewed this PR
      case 10:
        return "assignee";
      // The following column indices depend on whether a dashboard shows
      // the list of users who approved a PR.
      case 11:
        if (show_approvals) { return "approvals"; } else { return "lastUpdate"; }
      case 12:
        if (show_approvals) { return "lastUpdate"; } else { return "lastStatusChange"; }
      case 13:
        if (show_approvals) { return "lastStatusChange"; } else { return "totalTimeReview"; }
      case 14:
        if (show_approvals) { return "totalTimeReview"; } else { return idx; }
      default:
        return idx;
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
      // case "fromFork":
      case "labels":
        return 3;
      case "ciStatus":
        return 4;
      case "isBlocked":
        return 5;
      case "hasMergeConflict":
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
  // an "inverse" function to getIdx
  getAlias = function (idx) {
    switch (idx) {
      case 0:
        return "number";
      case 1:
        return "author";
      case 2:
        return "title";
      case 3:
        // return "fromFork";
        return "labels";
      case 4:
        return "ciStatus";
      case 5:
        return "isBlocked";
      case 6:
        return "hasMergeConflict";
      case 7:
        return "isReady";
      case 8:
        return "awaitingReview";
      case 10:
        return "missingTopicLabel";
      case 11:
        return "overallStatus";
      default:
        return idx;
    };
  }
}
const DEFAULT_LENGTH = 10;
function debounce(callback, delay = 500) {
  let timeout; // This variable is part of the closure
  return function(...args) { // The debounced function
    clearTimeout(timeout); // Clear any previous timeout
    timeout = setTimeout(() => {
      callback(...args); // Execute the original callback
    }, delay);
  };
}
function updateSearchParams(search) {
  const url = new URL(window.location.href);
  console.log('search', search);
  if (search === "") {
    url.searchParams.delete('search');
  } else {
    url.searchParams.set('search', search);
  }
  window.history.pushState({}, '', url.toString());
}
const debouncedUpdateSearchParams = debounce(updateSearchParams);
function optionsFromParams() {
  // Parse the URL for any initial configuration settings.
  // Future: use this for deciding which table to apply the options to.
  // let fragment = window.location.hash;
  const params = new URLSearchParams(document.location.search);
  const search_params = params.get("search");
  const pageLength = params.get("length") || DEFAULT_LENGTH;
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
    // push to sort_config in same order as in sort_params
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
  return ({ options, sort_config_approvals, sort_config, params });
}

$(document).ready(function () {
  const {options, sort_config_approvals, sort_config} = optionsFromParams();
  const tables = [];
  $('table').each(function () {
    let table;
    if (STANDARD) {
      const tableId = $(this).attr('id');
      let tableOptions = { ...options }
      const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
      tableOptions.order = show_approval ? sort_config_approvals : sort_config;
      table = $(this).DataTable(tableOptions);
    } else {
      const tableId = $(this).attr('id') || "";
      if (tableId.startsWith("t-")) {
        table = $(this).DataTable(options);
      }
    }

    // an object that tracks the number of times to disable the event handlers below during updates from popstate events
    const ignoreNext = { search: 0, length: 0, order: 0 };
    tables.push({table, ignoreNext});

    // event handlers to update params when table settings are changed
    $(this).on('search.dt', function (e, settings) {
      if (ignoreNext.search === 0) {
        debouncedUpdateSearchParams(settings.api.search());
      } else {
        ignoreNext.search--;
      }
    });
    $(this).on('length.dt', function (e, settings) {
      if (ignoreNext.length === 0) {
        const len = settings.api.page.len();
        const url = new URL(window.location.href);
        const paramsLength = url.searchParams.get("length") || DEFAULT_LENGTH;
        console.log('length', len);
        if (len !== paramsLength) {
          if (len === DEFAULT_LENGTH) {
            url.searchParams.delete('length');
          } else {
            url.searchParams.set('length', len);
          }
          window.history.pushState({}, '', url.toString());
        }
      } else {
        ignoreNext.length--;
      }
    });
    $(this).on('order.dt', function (e, settings) {
      if (ignoreNext.order === 0) {
        const order = settings.api.order();
        console.log('order', order);
        const url = new URL(window.location.href);
        // reset sort params in current URL
        url.searchParams.delete('sort');
        for (const [idx, dir] of order) {
          let alias;
          if (STANDARD) {
            const tableId = $(this).attr('id');
            const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
            alias = getAlias(idx, show_approval);
          } else {
            alias = getAlias(idx);
          }
          if (dir !== '') {
            url.searchParams.append('sort', `${alias}-${dir}`);
          }
        }
        window.history.pushState({}, '', url.toString());
      } else {
        ignoreNext.order--;
      }
    });
  });

  // handle query parameter changes when user clicks backwards / forwards in history
  // (popstate is not fired on pushState)
  $(window).on('popstate', function (event) {
    const {options, sort_config_approvals, sort_config, params} = optionsFromParams();
    console.log('popstate', params, options, sort_config_approvals, sort_config);
    // for each table, update search, length, and order settings
    for (const {table, ignoreNext} of tables) {
      if (params.has("search") && table.search() !== options.search.search) {
        ignoreNext.search++;
        table.search(options.search.search);
      } else if (!params.has("search") && table.search() !== '') {
        ignoreNext.search++;
        table.search('');
      }

      if (table.page.len() !== options.pageLength) {
        ignoreNext.length++;
        table.page.len(options.pageLength);
      }

      if (STANDARD) {
        const tableId = $(table).attr('id');
        const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
        ignoreNext.order++;
        ignoreNext.search++; // the draw() triggers a search
        table.order(show_approval ? sort_config_approvals : sort_config).draw();
      } else {
        ignoreNext.order++;
        ignoreNext.search++; // the draw() triggers a search
        table.order(sort_config).draw();
      }
    }
  });
});
