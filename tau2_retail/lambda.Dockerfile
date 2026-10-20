# A deployable Lambda image. The tau2 source is pinned so the endpoint and
# converted task data cannot silently drift apart.
FROM public.ecr.aws/lambda/python:3.12

ARG TAU2_COMMIT=96a3ef92329baf908ffe9dfe188cea34e8701d2a

RUN dnf install -y git && dnf clean all
# tau2 imports this optional voice dependency at package import time. Lambda's
# runtime process imports application dependencies from /var/task.
RUN git clone https://github.com/sierra-research/tau2-bench.git /opt/tau2-bench \
    && git -C /opt/tau2-bench checkout "${TAU2_COMMIT}" \
    && pip install --no-cache-dir --target "${LAMBDA_TASK_ROOT}" /opt/tau2-bench websockets

# tau2 resolves its domain data beside the source tree unless this is set.
ENV TAU2_DATA_DIR=/opt/tau2-bench/data

COPY tau2_retail/lambda_runtime ${LAMBDA_TASK_ROOT}/lambda_runtime

CMD ["lambda_runtime.handler.lambda_handler"]
