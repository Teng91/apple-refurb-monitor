const GITHUB_REPOSITORY = 'Teng91/apple-refurb-monitor';
const GITHUB_WORKFLOW = 'monitor.yml';
const GITHUB_REF = 'main';

/** Trigger the Apple refurbished monitor workflow on GitHub Actions. */
function triggerAppleRefurbMonitor() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('Missing GITHUB_TOKEN in Script Properties.');
  }

  const url = 'https://api.github.com/repos/' + GITHUB_REPOSITORY
      + '/actions/workflows/' + GITHUB_WORKFLOW + '/dispatches';
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: 'Bearer ' + token,
      'X-GitHub-Api-Version': '2026-03-10',
    },
    payload: JSON.stringify({ref: GITHUB_REF}),
    muteHttpExceptions: true,
  });

  const status = response.getResponseCode();
  const body = response.getContentText();
  if (status < 200 || status >= 300) {
    throw new Error('GitHub dispatch failed (' + status + '): ' + body);
  }
  console.log('GitHub workflow dispatched successfully (' + status + ').');
}

/** Install exactly one hourly trigger for the monitor. */
function installHourlyTrigger() {
  removeHourlyTrigger();
  ScriptApp.newTrigger('triggerAppleRefurbMonitor')
      .timeBased()
      .everyHours(1)
      .create();
  console.log('Hourly trigger installed.');
}

/** Remove this project's monitor triggers. */
function removeHourlyTrigger() {
  ScriptApp.getProjectTriggers()
      .filter(function(trigger) {
        return trigger.getHandlerFunction() === 'triggerAppleRefurbMonitor';
      })
      .forEach(function(trigger) {
        ScriptApp.deleteTrigger(trigger);
      });
}
