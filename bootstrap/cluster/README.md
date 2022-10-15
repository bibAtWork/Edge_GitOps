

## Setup

1. (Optional) Setup Windows-Subsystem for Linux (WSL)
	
	a) Open Windows Powershell in admin mode
	b) Execute the following command ```wsl --install -d Ubuntu```

	> Tip: The WSL filesystem can be open in the explorer with the following path ```\\wsl$```

2. Setup Ansible
	a) ```sudo apt install ansible```
	b) Install the Kubernetes command line tool ```[kubectl]( https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/ )```
	c) (optional) ```sudo apt install sshpass``` if the ssh connection is secured with a password.
	
3. Adopt the Ansible Inventory

4. Execute the k3s-ansible playbook

	ansible-playbook site.yml -i inventory/single-node/hosts.ini --ask-pass --ask-become-pass

5. Copy the Kubernetes config file

	a) create directory: mkdir ~/.kube
	b) copy file
	scp remote_user@remote_ip:~/.kube/config ~/.kube/config
	
	Example:
	scp tlabs@onsite-up2.fritz.box:~/.kube/config ~/.kube/config
	
	c) test cluster connection
	
	kubectl get namespaces

## Destroy

1. ansible-playbook reset.yml -i inventory/single-node/hosts.ini --ask-pass --ask-become-pass



## Additional Reading
- [WSL Install Guide]( https://learn.microsoft.com/de-de/windows/wsl/install )
- [Ansible Install Guide]( https://www.digitalocean.com/community/tutorials/how-to-install-and-configure-ansible-on-ubuntu-20-04 )
- [k3s-ansible]( https://github.com/k3s-io/k3s-ansible ) 