# homepage-test

Manual (non-flux) scratch deploy for testing homepage versions/config next to prod.
Flux `homepage` Kustomization is scoped to `./app` so this dir is ignored.

Apply:
  kubectl apply -k ~/git/k8s/cluster-talos/kubernetes/apps/tools/homepage/test

Tear down:
  kubectl delete -k ~/git/k8s/cluster-talos/kubernetes/apps/tools/homepage/test

URL: https://testhome.boeye.net
