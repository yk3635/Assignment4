1. If it's set on a running pod/container:

bash
# Find which pod has this env var
kubectl get pods -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{range .spec.containers[*]}{.env}{"\n"}{end}{"\n"}{end}' | grep -B5 CASC_JENKINS_CONFIG

# Or more simply, once you know the pod:
kubectl describe pod <jenkins-pod-name> -n <namespace> | grep -A2 CASC_JENKINS_CONFIG

# Check the deployment/statefulset spec directly
kubectl get deployment <jenkins-deployment> -n <namespace> -o yaml | grep -B10 CASC_JENKINS_CONFIG

2. If it's set via a ConfigMap or Secret:

bash
# Search all ConfigMaps in the namespace
kubectl get configmap -n <namespace> -o yaml | grep -B5 CASC_JENKINS_CONFIG

# Search all Secrets (values will be base64 encoded)
kubectl get secret -n <namespace> -o yaml | grep -B5 CASC_JENKINS_CONFIG

. If it's set via Helm values (likely, given this looks like a Helm-deployed Jenkins):

bash
# Check the Helm release values
helm get values <release-name> -n <namespace>

# Or check the full rendered manifest
helm get manifest <release-name> -n <namespace> | grep -B10 CASC_JENKINS_CONFIG

4. If it's in your GitLab-managed IaC/GitOps repo (likely, since ArgoCD manages your clusters):

bash
# Search your infra repo locally
grep -rn "CASC_JENKINS_CONFIG" /path/to/repo

# Or via GitLab's search UI/API if you don't have it cloned locally

5. Trace it all the way back — check ArgoCD:

bash
# Since you use ArgoCD, find which Application manages this
kubectl get application -n argocd | grep -i jenkins
argocd app manifests <app-name> | grep -B10 CASC_JENKINS_CONFIG

Quickest single command if you just want the source fast:

bash
kubectl get deploy,sts,cm,secret -n <namespace> -o yaml | grep -B15 CASC_JENKINS_CONFIG

This dumps everything in the namespace and greps with context, so you can see exactly which resource and field it's coming from in one shot.



If it's #1 (surprised it updated so fast): this env var is set from the pod spec's env block, which templates from the ConfigMap/Secret. If the pod was recreated (not just the ConfigMap edited), Kubernetes would pick up the new value on pod start. Worth checking:

bash
kubectl get pod <jenkins-pod> -n <namespace> -o jsonpath='{.metadata.creationTimestamp}'

bash
grep -rn "ref:" . --include="*.yaml" | grep -i gitlab


kubectl get secret <jenkins-secret-name> -n <namespace> -o jsonpath='{.data.CASC_JENKINS_CONFIG}' | base64 -d

kubectl get secret <jenkins-secret-name> -n <namespace> -o jsonpath='{.data.CASC_JENKINS_CONFIG}' | base64 -d

