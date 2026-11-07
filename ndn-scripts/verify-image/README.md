# Verify the Received Image

Mount the `function-invoke-output` PVC in a temporary Pod to retrieve the saved image.
Run the following commands from the repository root on the SSH host.

## Start the Pod

Check which node runs the Invoker.

```bash
kubectl -n ndn get pods -l app=function-invoke -o wide
```

The `nodeSelector` in `image-export.yaml` specifies `kube5`.
If the Invoker is still using the PVC, set it to the same node.

```bash
kubectl apply -f ndn-scripts/verify-image/image-export.yaml
kubectl -n ndn wait --for=condition=Ready pod/image-export --timeout=120s
```

## Retrieve the Image

```bash
mkdir -p /home/ryu/Downloads/argocd-invoke-results
kubectl -n ndn cp image-export:/output/use_print_image.bin \
  /home/ryu/Downloads/argocd-invoke-results/use_print_image.png
```

The `.bin` file contains the original PNG bytes, so save it with a `.png` extension.

To copy the image to your local computer, replace `<SSH_HOST>` and run these commands in your local terminal.

```bash
scp ryu@<SSH_HOST>:/home/ryu/Downloads/argocd-invoke-results/use_print_image.png ~/Downloads/
open ~/Downloads/use_print_image.png
```

## Clean Up

Delete the temporary Pod on the SSH host. The PVC and saved image remain intact.

```bash
kubectl -n ndn delete pod image-export
```
