// nselive.pages.dev: the short address of the chart. Every request goes, unchanged, to the nse-live-chart
// Worker (a service binding, not the internet), which serves the sign-in, the chart and its API.
export default {
  fetch(request, env) {
    return env.CHART.fetch(request);
  },
};
