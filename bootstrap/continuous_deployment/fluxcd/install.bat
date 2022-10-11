@ECHO ON

TITLE FluxCd automated setup on Windows


REM Request GitHub user and password for Repository READ and WRITE
@ECHO OFF
set /P TF_VAR_github_owner=Enter GitHub User or Organization [string]: 
set /P TF_VAR_github_token=Enter Token [string]: 
@ECHO ON

REM Installation of Flux CD on the Kubernetes cluster
cd %~dp0 ^
  && terraform destroy -auto-approve ^
  || del *terraform*

REM Install FluxCD and link it to a Git repository
cd %~dp0flux_sync ^
  && terraform init ^
  && terraform apply -auto-approve ^
  && echo "INFO: Linking of Flux CD to Git repository was successful."
if %errorlevel% neq 0 exit /b %errorlevel%
