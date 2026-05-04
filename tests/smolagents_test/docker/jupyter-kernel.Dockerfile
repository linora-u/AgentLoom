FROM vemlp-cn-beijing.cr.volces.com/preset-images/code-sandbox:server-20250609

RUN pip install --no-cache-dir jupyter_kernel_gateway jupyter_client ipykernel

EXPOSE 8888

CMD ["jupyter", "kernelgateway", "--KernelGatewayApp.ip=0.0.0.0", "--KernelGatewayApp.port=8888", "--KernelGatewayApp.allow_origin=*"]
