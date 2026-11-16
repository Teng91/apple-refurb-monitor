const GITHUB_REPOSITORY = 'Teng91/apple-refurb-monitor';
const GITHUB_WORKFLOW = 'monitor.yml';
const GITHUB_REF = 'main';
const MONITOR_INTERVAL_HOURS = 1;
// Daily health notice at this hour, Taipei time (Apps Script fires within about 15 minutes of it).
const HEALTH_HOUR = 0;

/** Trigger the Apple refurbished monitor workflow on GitHub Actions. */
function triggerAppleRefurbMonitor() {
  dispatchMonitor_(false);
}

/** Daily health check: GitHub checks Apple first, then sends a Telegram health notice. */
function triggerDailyHealthCheck() {
  dispatchMonitor_(true);
}

function dispatchMonitor_(healthCheck) {
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
    payload: JSON.stringify({ref: GITHUB_REF, inputs: {health_check: healthCheck}}),
    muteHttpExceptions: true,
  });

  const status = response.getResponseCode();
  const body = response.getContentText();
  if (status < 200 || status >= 300) {
    throw new Error('GitHub dispatch failed (' + status + '): ' + body);
  }
  console.log('GitHub workflow dispatched successfully (' + status + ').');
}

/**
 * Install exactly one monitor trigger (every MONITOR_INTERVAL_HOURS hours) and one daily
 * health trigger (HEALTH_HOUR, Taipei time). Safe to run again: old triggers are removed first.
 */
function installMonitorTrigger() {
  removeMonitorTriggers();
  ScriptApp.newTrigger('triggerAppleRefurbMonitor')
      .timeBased()
      .everyHours(MONITOR_INTERVAL_HOURS)
      .create();
  ScriptApp.newTrigger('triggerDailyHealthCheck')
      .timeBased()
      .inTimezone('Asia/Taipei')
      .atHour(HEALTH_HOUR)
      .nearMinute(0)
      .everyDays(1)
      .create();
  console.log('Triggers installed: monitor every ' + MONITOR_INTERVAL_HOURS
      + ' hour(s), health check daily at ' + HEALTH_HOUR + ':00 Asia/Taipei.');
}

/** Remove this project's monitor and daily health triggers. */
function removeMonitorTriggers() {
  const handlers = ['triggerAppleRefurbMonitor', 'triggerDailyHealthCheck'];
  ScriptApp.getProjectTriggers()
      .filter(function(trigger) {
        return handlers.indexOf(trigger.getHandlerFunction()) !== -1;
      })
      .forEach(function(trigger) {
        ScriptApp.deleteTrigger(trigger);
      });
}
