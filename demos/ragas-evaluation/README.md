# 🚀 RAGAS Evaluation Demo with SDG Hub RAG Flow

## Overview

This demo showcases how to generate synthetic RAG evaluation datasets using the SDG Hub RAG Flow and evaluate them using the RAGAS Llama Stack Eval Provider developed by the Trusty AI team. The demo demonstrates:

1. **Synthetic Dataset Generation**: Using SDG Hub to create question-answer pairs with ground truth context from documents
2. **RAGAS Evaluation**: Using the RAGAS provider to evaluate RAG systems with metrics like faithfulness, answer relevancy, context precision, and context recall

The demo includes notebooks for dataset generation and evaluation, running on Red Hat OpenShift AI.

This guide assumes RHOAI 3.0+ is installed on an OpenShift 4.19.9+ cluster.

> Note: This demo was tested using the default KServe behavior on OpenShift AI (`Headless` RawDeployment). If you are using `Headed` mode, change the `VLLM_URL` port to `80` in the [llama-stack-distribution.yaml](deployment-yamls/llama-stack-distribution.yaml).

---

## Table of Contents

- [Initial Setup (Follow Red Bank Demo)](#initial-setup-follow-red-bank-demo)
- [Deploy AI Models and Llama Stack Server](#deploy-ai-models-and-llama-stack-server)
- [Run the Evaluation Flow](#run-the-evaluation-flow)
  - [Generate Synthetic Dataset](#generate-synthetic-dataset)
  - [Run RAGAS Evaluation](#run-ragas-evaluation)

---

## Initial Setup (Follow Red Bank Demo)

Before proceeding with the RAGAS evaluation demo, you need to complete the initial setup steps. Please follow the [Red Bank Demo](../redbank-demo/README.md) and complete the following sections:

1. **[Instructions to get GPUs on OpenShift](../redbank-demo/README.md#instructions-to-get-gpus-on-openshift)**
   - Get GPU Worker Nodes
   - Install the GPU Operators
   - Accelerator Migration (if needed)

2. **[Update RHOAI DSC](../redbank-demo/README.md#update-rhoai-dsc)**
   - Enable the Llama Stack K8s Operator

3. **[Create and Configure a Data Science Project](../redbank-demo/README.md#create-and-configure-a-data-science-project)**
   - Create a project named `ragas-evaluation` (instead of `redbank-demo`)
   - Create a workbench named `ragas-evaluation-workbench` (instead of `redbank-workbench`)

4. **[Create Data Science Pipeline Server](../redbank-demo/README.md#create-data-science-pipeline)**
   - Create AWS S3 Bucket (name it `ragas-evaluation-s3-bucket` or similar)
   - Create Data Science Pipeline Server (use the same AWS credentials and region)
   - **Important:** After configuring the pipeline server, get the pipeline endpoint URL:
     ```bash
     echo "https://$(oc get route ds-pipeline-dspa -n ragas-evaluation -o jsonpath='{.spec.host}')"
     ```
     Save this URL as you'll need it in the next section.

Once you've completed these steps, return to this guide and continue with the [Deploy AI Models and Llama Stack Server](#deploy-ai-models-and-llama-stack-server) section below.

---

## Deploy AI Models and Llama Stack Server

> **Note:** The Qwen3-14B-AWQ model uses 1 GPU. This demo has been tested with `g5.2xlarge` nodes (A10g NVIDIA GPU).

**Before deploying, update the following cluster-specific YAML files:**

1. **Update AWS Credentials** (`deployment-yamls/aws-credentials.yaml`):
   - Replace the empty values with your AWS credentials:
     - `AWS_ACCESS_KEY_ID`: Your AWS access key
     - `AWS_SECRET_ACCESS_KEY`: Your AWS secret key
     - `AWS_DEFAULT_REGION`: Your AWS region (e.g., `us-east-1`)

2. **Update Kubeflow RAGAS Config** (`deployment-yamls/kubeflow-ragas-config.yaml`):
   - Update `KUBEFLOW_PIPELINES_ENDPOINT`: Replace with your pipeline server endpoint URL (obtained during the initial setup step above). If you need to retrieve it again, run:
     ```bash
     echo "https://$(oc get route ds-pipeline-dspa -n ragas-evaluation -o jsonpath='{.spec.host}')"
     ```
   - Update `KUBEFLOW_RESULTS_S3_PREFIX`: Set to your S3 bucket path (e.g., `s3://ragas-evaluation-s3-bucket/ragas-results`)

3. **Create Kubeflow Pipelines Token Secret:**

   Unfortunately, the Llama Stack distribution service account does not have permission to create pipeline runs. In order to work around this, we must provide a user token as a secret to the Llama Stack Distribution.

   Create the secret with:

   ```bash
   oc create secret generic kubeflow-pipelines-token \
     --from-literal=KUBEFLOW_PIPELINES_TOKEN=$(oc whoami -t) \
     -n ragas-evaluation
   ```

**Steps:**

1. Run in your terminal:
```bash
  make deploy-all
```

**Verification:**

Wait until all pods are fully running in the `ragas-evaluation` namespace. You can check the status with:

```bash
oc get pods -n ragas-evaluation
```

You should see:
- `qwen3-14b-awq-predictor-*` (inference model)
- `lsd-ragas-example-*` (Llama Stack Distribution)

---

## Run the Evaluation Flow

### Generate Synthetic Dataset

**Steps:**

1. Go to the Red Hat OpenShift AI dashboard, and go into your workbench. This will open your JupyterNotebook environment.
2. Upload the `notebooks` directory to your JupyterNotebook environment.
3. Open the `1.dataset_generation.ipynb` file.
4. Follow the notebook steps to:
   - Prepare your input dataset (documents with outlines)
   - Configure the SDG Hub RAG Flow
   - Generate synthetic question-answer pairs with ground truth context
   - Post-process the results for evaluation

**Note:** The notebook includes an example using the IBM Annual Report 2024 PDF (`ibm-annual-report-2024.pdf`). You can use your own documents by modifying the input dataset preparation section.

---

### Run RAGAS Evaluation

**Steps:**

1. In the same JupyterNotebook environment, open the `2.ragas-evaluation.ipynb` file.
2. Follow the notebook steps to:
   - Set up the Llama Stack client
   - Prepare your evaluation dataset (from the previous notebook or your own dataset)
   - Run RAGAS evaluation metrics
   - Visualize and analyze the results