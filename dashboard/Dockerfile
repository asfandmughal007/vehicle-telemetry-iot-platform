# syntax=docker/dockerfile:1
# Static dashboard served by an unprivileged nginx (listens on 8080 inside the container).
#   docker build -f dashboard/Dockerfile -t telemetry-dashboard .
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY dashboard/index.html dashboard/style.css dashboard/app.js dashboard/lib.js dashboard/config.js /usr/share/nginx/html/
EXPOSE 8080
