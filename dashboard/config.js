// Runtime configuration. In AWS, replace apiBaseUrl with the API Gateway URL
// (CDK output "ApiUrl"); this file is mounted/overwritten at deploy time.
window.DASHBOARD_CONFIG = {
  apiBaseUrl: "http://127.0.0.1:8080",
  refreshSeconds: 5,
  historyPoints: 120,
};
